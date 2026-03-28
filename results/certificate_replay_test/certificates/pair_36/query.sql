SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON a.personid = p.personid AND 1 = 1