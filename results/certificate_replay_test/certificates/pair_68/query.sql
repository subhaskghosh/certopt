SELECT
  firstname,
  lastname,
  city,
  state
FROM PERSON AS P
LEFT JOIN ADDRESS AS A
  ON personid = a.personid