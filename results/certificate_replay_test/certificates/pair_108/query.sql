SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON p.personid LIKE a.personid