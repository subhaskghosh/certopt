SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS D
  ON d.personid = p.personid